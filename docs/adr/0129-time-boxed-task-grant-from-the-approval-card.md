# ADR-0129: A time-boxed task grant from the approval card

- Status: Accepted by the owner 2026-09-25
- Date: 2026-09-25
- Related: ADR-0017, ADR-0058, ADR-0098, ADR-0099, ADR-0106, ADR-0111,
  ADR-0123, ADR-0127, ADR-0128, ADR-0130
- Amends: ADR-0058 decision 17 (a grant "categorically refuses hard-excluded
  or unknown consequences"); ADR-0127 decision 3 (the refusals the runtime
  gives before dispatch gain `tool.browser.grant_not_applicable`); Section 9.3
  of the engineering plan ("Do not implement session-wide or permanent
  approval grants initially"); `docs/plan/policy-and-approvals.md`
  (`ApprovalResolutionType` has exactly two values);
  `docs/plan/http-api-and-streaming.md` (the two-value decision vocabulary);
  `docs/plan/browser-automation.md` (a grant covers only `routine`
  interaction, and browser gate 10)
- Detailed design: `docs/plan/browser-automation.md`,
  `docs/plan/policy-and-approvals.md`, `docs/plan/http-api-and-streaming.md`

## Context

Every `browser.act` stops for an approval. The card reads "Run browser.act with
validated arguments." and shows an opaque element reference, so the owner
approves blind. A language lesson needs roughly seventy to a hundred actions,
so it cannot finish one approval at a time.

The existing standing grant cannot help:

- It needs a deployment-wide `BROWSER_PROFILE_ID` and `BROWSER_GRANT_ID` pin.
  Production binds its profile per chat session, so the grant never applies.
- It covers only `routine` actions: a click on a button named one of ten words
  such as "Continue", or a scroll. A lesson's answers are words the classifier
  cannot know, so they are `unknown`, and `unknown` is hard-excluded
  (`browser-automation.md:628`).
- Its consequence check reads the observation cached in the worker. Nothing in
  the isolated browser checks it again against the live page.

The owner decided on 2026-09-25:

> From the first approval you allow one lesson: clicks and typing on
> duolingo.com/lesson for 30 minutes, up to 200 actions. Passwords, payments,
> account settings and messages still ask every time.

The owner also required the approval card to show what is being clicked: the
element's role and quoted name, and the page's origin and path.

A security review of the first draft found that it offered a task grant on any
site's first path segment, which is wider than the owner approved, and that
the classifier and the card read only one of an element's labels. The owner
then decided, also on 2026-09-25, that task grants are offered only inside
site scopes the owner configures, and that the production deployment lists
exactly one: `https://www.duolingo.com` with the path prefix `/lesson`.

This loosens a security rule, `browser-automation.md:591` and `:628`. That
change needs the owner's explicit approval, and the owner has given it.

## Decisions

1. **A task grant stands in for approvals in one chat, and the owner creates
   it from the approval card.**
   - A `browser.act` approval can carry a server-authored offer: one
     configured site scope (an exact origin and a path prefix), thirty
     minutes and two hundred actions.
   - The owner accepts it by resolving the approval with the new resolution
     `approve_for_task`. That approves the pending action once and creates a
     `BrowserTaskGrant` in the same transaction.
   - Nothing else can create a task grant. There is no create route, and the
     model, page content, the CLI, inbound surfaces and notification actions
     cannot create one.
   - `approve_for_task` needs `approval.resolve` and `browser.grant.write`.
     Both scopes already exist, so the scope vocabulary does not change.
   - The request repeats the offered origin and path prefix. If they differ
     from the stored offer, or the offer is no longer valid, the request is
     refused with `409`. The approval then stays pending, so the owner can still
     choose Allow once.

2. **Task grants are offered only inside site scopes the owner configures.**
   - `BROWSER_TASK_GRANT_SCOPES` is a deployment setting: a comma-separated
     list of exact site scopes, each written as an origin and one path
     segment, such as `https://www.duolingo.com/lesson`. It is empty by
     default, and an empty list means no offer is ever made.
   - An offer is made only when the page the pending action was observed on
     has a configured scope's exact origin and lies inside its path prefix. The
     grant's origin and prefix are the configured entry, never values taken
     from the page URL, so a page cannot choose or widen its own grant.
   - Configuration refuses a scope that is not public HTTPS, has a query,
     fragment or credentials, has more or fewer than one path segment, or
     whose segment is sensitive (decision 5).
   - Adding a scope is an owner configuration change that widens authority,
     made deliberately in the deployment environment. Removing a scope ends
     every active grant for it at its next authorization, with reason
     `scope_removed`.
   - The code names no site. Production is set to exactly
     `https://www.duolingo.com/lesson`.

3. **The grant is bound to the chat session, not the run.**
   - It pins the tenant, principal, session and browser profile. It also pins
     the profile generation, agent version, policy version, origin, path
     prefix, creation and expiry times, the action cap and the approval that
     created it.
   - Only an interactive top-level run can use it, in a session that carries
     the trusted `browser_profile_id` binding and no schedule binding, and
     whose newest user message came from the owner. Session creation is the
     only writer of that binding. Scheduled runs, inbound-surface and
     device-ingested sessions, delegated child runs and other sessions cannot
     use a task grant. Scheduled occurrences run as `interactive` runs in
     sessions of their own, so the session binding, not the run kind, is what
     excludes them.
   - Session scope lets a lesson continue after a run ends (budget, detector,
     or the owner saying "continue"). The caps bound what that adds.
   - A session has at most one active task grant. A new one ends the old one
     with reason `superseded`.

4. **The grant's limits are fixed and cannot be configured.**
   - It lasts thirty minutes from resolution and covers at most two hundred
     grant-authorized actions and 4,096 typed characters in total. These are
     code constants, and database check constraints enforce them.
   - The action approved on the card does not count toward either cap.
   - An authorization consumes one use, and the typed characters, atomically
     before dispatch. A use is not refunded if the action later fails, so
     `actions_used` is an upper bound on actions actually sent.

5. **One shared, deny-biased classifier decides consequence and coverage.**
   - `agent_core.domain.browser_classification` replaces
     `_classify_consequence`. It maps an action to a
     `BrowserActionConsequence` from:
     - the element's role and every label source it has: `aria-label`, the
       text of its `aria-labelledby` targets, its associated `<label>`
       elements, `title`, `placeholder`, `alt`, a button's `value`, and its
       visible text;
     - the accessible name of any enclosing dialog;
     - for a select, the chosen option's label and value;
     - a closed field kind;
     - the navigation target of a link or form, including every segment of
       its path.
   - Each label source is classified separately. A match of the exclusion
     vocabulary in any of them yields that named consequence, so a button
     labelled "Continue" for assistive technology but showing "Pay $12.99"
     is a payment.
   - The vocabulary covers payment, currency and in-app currency (gems,
     coins), purchase, subscription and trial, money movement, authentication
     and signing out, account recovery, account and settings changes,
     permissions, legal acceptance, messages, posts and publication, deletion,
     and file transfer. Each word also matches its inflected forms, including
     irregular ones such as "paid", "bought", "sold", "sent" and "spent".
   - A path segment is sensitive when it matches the vocabulary or names an
     administrative, authentication, pricing or messaging area. Every segment
     of the page path and of a link or form target is checked, not only the
     first.
   - Every word the old hard-exclusion list held yields a named consequence.
   - A standing grant still covers only `routine`, and only when every label
     source reads as the same routine word.

6. **A task grant covers `routine` and `unknown` interaction, within strict
   rules.**
   - It covers click, select, check, press, scroll, and typing into text,
     search and multi-line fields. It never covers a named consequence.
   - Typed text is covered only when it is at most 256 characters, contains
     no `@`, `://` or `www.`, no run of four or more digits and nothing shaped
     like a credential, and fits the grant's remaining 4,096 characters.
   - It covers an action only in a model turn whose tool calls, since the
     owner's newest message, are all `browser.navigate`, `browser.observe` or
     `browser.act`, directly or through `tool.call`. A turn that has read
     email, memory through a tool, knowledge or anything else goes back to
     the approval card.
   - It never covers:
     - credential, one-time-code, payment or identity fields;
     - key presses on choice controls, since an arrow key checks another
       radio in the group without classifying it; select and check still
       cover them;
     - key presses or typed text on an element that does not itself hold
       focus once focused, since the keyboard sends to the focused element
       (decision 7);
     - unnamed elements;
     - links or form submissions whose target leaves the origin or the
       prefix, or has a sensitive path segment. Any click on an element in a
       form, and Enter or Space pressed on one, counts as a submission,
       whatever the element's role. The target is where the browser would
       submit, including a submit button's `formaction`;
     - download links and file inputs;
     - a page outside the prefix, or one with a sensitive path segment.
   - Credential and one-time-code fields stay refused outright, as before.

7. **Checks run in two places: the worker decides, and the isolated runtime
   can only refuse.**
   - The worker authorizes against the observation that named the element.
     That observation now carries secret-free facts beside it: the label
     sources, and each element's field kind, navigation target, download
     flag and dialog name. These facts never enter a model-visible result.
   - An authorized act carries a `BrowserDispatchConstraint` to the isolated
     service: the grant kind, origins, the path prefix, `not_after`, the
     consequence ceiling and the per-action text cap. The grant kind fixes
     the last two, and the service refuses a constraint whose fields
     disagree with it.
   - The runtime reads the live page URL, the live element and its live label
     sources, and runs the same classifier and coverage rules.
   - A key press or typed text goes to whatever holds focus, not to the
     element the classifier read. For those, the runtime focuses the element
     and refuses before dispatch unless the element itself then holds focus,
     resolved through open shadow roots; an embedded document never does.
     Until the key or text is sent, the runtime stops any key or text event
     aimed at another element, with its default action, and then reports the
     act's outcome as unknown, so focus the page moves after the check cannot
     redirect the action.
   - A mismatch refuses the action before dispatch with the new stable code
     `tool.browser.grant_not_applicable`. It is a refusal given before
     dispatch in the sense of ADR-0127 decision 3: the lease and its action
     sequence are unchanged. The runtime and the provider also discard the
     observation, so the model must observe again, and its next proposal is
     judged on fresh facts and reaches an ordinary approval.
   - The constraint can only narrow what an act may do. An act without one
     behaves exactly as before.
   - The pinned standing grant passes the same kind of constraint, with a
     `routine` ceiling.

8. **The `browser.act` approval card shows what the action does.**
   - A new in-session approval hook (`approval_view_in_session`) lets
     `browser.act` build its approval view from the session's latest
     observation and facts. The view shows:
     - the action kind;
     - the element role;
     - the element's accessible name, and its visible text when that differs,
       both quoted;
     - any enclosing dialog name, quoted;
     - the page origin and path, never the query or fragment;
     - the page title, quoted;
     - the key, scroll distance, option or typed text.
   - The name, visible text, dialog name, title and option come from the
     website. The client renders them quoted and labelled as coming from the
     website.
   - `action_summary` contains only server-authored words and trusted values,
     such as "Click a button on www.duolingo.com/lesson".
   - The offer text says that Veetbot recognises payments, purchases,
     account changes and messages by the website's labels, and that the owner
     can stop the grant at any time. It does not promise more than that.
   - The view never contains the element reference, the page revision, a
     query string, a cookie or a credential.
   - Typed text is shown, because the owner cannot judge a typing action
     without it. The value is already in the owner's own invocation record, so
     showing it adds no exposure.
   - Text typed into a password, one-time-code or payment field, and any
     credential-shaped value, is shown as `[REDACTED]`. Text over 512
     characters is truncated and published with its digest (ADR-0099).
   - If the worker can no longer describe the element, the view says so and
     makes no offer.

9. **Grants are audited, listed and revocable.**
   - These events are appended:
     - `browser.task_grant.created`, together with `approval.resolved`;
     - `tool.call.authorized` for each use, carrying `authorization_kind:
       browser_task_grant`, the grant id, the use ordinal, and the same view
       of the action the approval card would have shown, so every action no
       one reviewed is recorded as what it was;
     - `browser.task_grant.ended`, exactly once, with reason `expired`,
       `exhausted`, `revoked`, `superseded`, `profile_changed`,
       `profile_revoked`, `policy_changed`, `agent_changed` or
       `scope_removed`.
   - A maintenance sweep ends expired grants within one approval-reaper
     interval.
   - `GET /v1/browser-task-grants`, `GET /v1/browser-task-grants/{id}` and
     `POST /v1/browser-task-grants/{id}/revoke` use `browser.grant.read` and
     `browser.grant.write`. They are mounted only when
     `BROWSER_TASK_GRANTS_ENABLED` is set, so the Milestone 5 route census is
     unchanged.
   - Revocation takes effect at the next authorization. Revoking or deleting
     the profile ends every task grant that refers to it.
   - When an active grant does not cover an action, the resulting approval
     records the reason, so the card can say why it is asking.

10. **A new sign-in ends task grants.** Every sign-in, remote or on the
    owner's device, advances the profile generation when it begins and again
    when it becomes ready (ADR-0128 decision 10). The profile management
    service ends every active task grant on that profile with reason
    `profile_changed` in the same unit of work, and the authorizer also
    refuses any grant whose pinned generation no longer matches. A grant
    therefore never outlives a change of the account behind the profile.

11. **The pinned standing grant is checked first.** A composite authorizer
    asks the pinned standing grant first, then the task grant. The first allow
    wins, and each records its own kind. Both run only after the deterministic
    engine returns `REQUIRE_APPROVAL`, and they re-run it. A policy `DENY`, a
    hardline rule and an advisory escalation are never overridden. The task
    grant, like the standing grant, satisfies the external-untrusted trust
    overlay; otherwise no browser grant could ever apply.

12. **Rollout is behind a flag and ships to a development branch first.**
    - `BROWSER_TASK_GRANTS_ENABLED` is off by default. When it is off, no
      offer is made, `approve_for_task` is refused and the task-grant routes
      do not exist. `BROWSER_TASK_GRANT_SCOPES` requires the flag.
    - The new approval card for `browser.act` is not flagged.
    - No policy YAML changes, so `policy_version` and memory-formation
      evidence are unaffected. No versioned configuration knob is added.
    - Worker and isolated service stay compatible during a release, which
      starts the new isolated service before the workers: facts travel as an
      optional sibling of the observation that an older worker ignores, and
      an absent fact means no coverage.
    - Production turns the flag on only after a client build that understands
      `approve_for_task` is installed on every device the owner uses; an
      older build cannot decode an approval resolved that way.
    - The work stays on a development branch until the whole lesson path is
      built and verified. Production deploys only from `main`, through a later
      `main` pull request.

## Consequences

- A lesson can run with one approval. The owner sees what each approval card
  acts on, and a covered action runs without a card for at most thirty
  minutes, two hundred actions and 4,096 typed characters, only inside a
  scope the owner configured.
- **Deliberate loosening.** A task grant accepts `unknown`-consequence actions,
  which no grant accepted before. Page content can therefore steer the model
  into actions inside the configured scope that no one reviews. The bounds
  are:
  - the owner-configured origin and path prefix, checked on every path segment
    against the live page;
  - the classifier's exclusions over every label source;
  - refusal of credential, payment and identity fields;
  - typed-text shape limits and a browser-only turn;
  - the time, action and text caps;
  - revocation, and the end of every grant on a new sign-in;
  - the audit trail, which records what each unreviewed action was.
- **Limits of name matching.** The exclusion vocabulary is English and reads
  the DOM. A site that names its controls in another language, disguises them
  with lookalike characters, or draws a label with CSS or an image without
  alternative text can defeat name matching. The configured path prefix is
  then the real boundary. This is why scopes are the owner's choice and are
  suited to sites the owner trusts not to disguise their controls.
- **False positives are expected.** An answer tile that happens to be "pay",
  "post", "card" or "changed" asks for approval, and so does typed text with
  four digits in a row. This is the deny-biased direction, and the rollout
  measures how often it happens.
- **Schema and API additions.** There is one new table, one resolution value,
  three flag-mounted routes, three optional approval fields, two event types,
  one reason code and two environment settings. There are no new scopes and no
  versioned knobs. `browser.grant.read` and `browser.grant.write` must be
  present in production `AUTH_SCOPES`.
- **Gate.** Browser gate 10 is renamed "Standing and task grants" and extended
  in place. Its id and the gate count are unchanged.
- **Related work.** Lease renewal (ADR-0127), the on-device sign-in
  (ADR-0128) and the browser task budget (ADR-0130) are separate decisions.
  A deferred `browser.act` (ADR-0123) is unwrapped before policy, so grants
  apply to it unchanged. ADR-0131 landed on `dev` first; the event-catalogue
  count this ADR changes is computed on top of it.

## Alternatives

- **Offering a grant on any site and any first path segment.** This was the
  first draft. On a banking profile it would have offered a grant on
  `/transfer`, and the page URL, which the site controls, chose the grant's
  prefix. Rejected.
- **A scope table fixed in code.** It would make adding a site a code change
  and put a site name in the code. The owner chose a deployment setting, so
  that adding a scope is the owner's own configuration act. Rejected.
- **Widening the standing grant to `unknown`.** A standing grant is
  deployment-wide, lasts up to thirty days and is pinned by an operator. It
  would lend unreviewed authority to every chat. Rejected.
- **Binding the grant to one run.** This is tighter, but the lesson would stop
  at any run boundary and the owner would approve again. Session binding with
  hard caps was chosen.
- **A route for creating grants directly.** The approval card is the only
  place where the owner has just seen a concrete action on the concrete page.
  A separate route would invite grants made without that context. Rejected.
- **Checking only in the worker.** The worker's observation can be stale by the
  time the action is sent. The owner required a check against the live page,
  and the runtime makes one.
- **Asking the runtime to allow actions, not just refuse them.** Authority
  stays with the worker and the database. A constraint that can only narrow
  lets a stale or forged constraint cause nothing worse than a refusal.
- **Refusing grants whenever the chat's context holds memories.** The context
  builder marks every step that carries a memory snapshot or recall as
  memory-derived, which is nearly every Chat step, so this would disable
  grants outright. The browser-only turn rule and the typed-text limits close
  the path by which recalled private data would be typed. Rejected.
- **Masking typed text.** The owner could not judge what would be typed, and
  the value is already in the owner's own records. Rejected, except for
  sensitive fields.
- **Making the duration and caps configurable.** Configuration would widen
  authority silently. Rejected; only the scope list is configurable, and it
  names where, not how much.

## Validation

Red tests come first. Import, fixture and environment errors are not red
tests. The coverage required:

- The approval-resolution regression, before anything else: the approval
  repositories store every resolution that is not `approve_once` as `DENIED`,
  so `approve_for_task` must resolve to `APPROVED` in memory and in
  PostgreSQL before the value can be accepted anywhere.
- A table-driven classifier suite covering each vocabulary entry and its
  inflected forms, every label source (including a "Continue" `aria-label`
  over visible "Pay $12.99"), case, hyphenation, zero-width and combining
  variants, currency symbols, field kinds, every path segment, navigation
  targets and typed-text shapes. It includes a monotonicity property: adding
  an excluded word never makes an action covered.
- Scope-configuration tests: an empty list makes no offer; a sensitive,
  multi-segment, non-HTTPS or duplicate scope is refused at startup; a
  `READY` profile on another origin at `/lesson` gets no offer.
- Repository contract and PostgreSQL suites. Concurrent use grants exactly two
  hundred actions and never more than 4,096 typed characters. Expiry,
  revocation, supersession and profile changes each end the grant exactly
  once. Deleting the session removes its grants in both adapters.
- Boundary tests for resolve, list, get and revoke: the happy path,
  validation, authorization, a cross-principal `404`, a failure, and a retry.
  A route walk shows the three routes only with the flag on, and the
  Milestone 5 census stays thirty-one with it off.
- Isolated-service tests: an expired or foreign-origin constraint and a live
  page, element or label that no longer qualifies are refused before
  dispatch; the refusal leaves the lease open and the sequence unchanged; a
  replay of an applied sequence, with or without a constraint, is `409` and
  dispatches nothing; a malformed constraint is `400` and dispatches nothing.
- Pipeline tests showing:
  - a second action runs with no approval;
  - the two-hundred-and-first action asks, and so does text past 4,096
    characters;
  - a revoked, expired, other-session, scheduled-session or child-run grant
    asks, and so does a turn that called a non-browser tool;
  - a sign-in on the bound profile ends the grant;
  - a policy `DENY` or hardline denial is never overridden.
- An approval-view suite showing:
  - no reference or revision appears;
  - the visible text appears beside a differing accessible name;
  - typed text is shown, or redacted for a sensitive field;
  - the summary is free of page text.
- A real-browser local integration test: the isolated service's Playwright
  runtime in real Chromium, against a synthetic HTTPS lesson site served
  locally, refuses a grant-constrained act after the page moves outside the
  prefix, after a button is renamed, and on a hidden label, and dispatches a
  covered one.
- `tests/gates/test_browser_m10.py::test_standing_grant` gains the task-grant
  cases.

Agents verify with these automated tests only. Production deploys only from
`main`, and the owner chose to keep this work on a development branch until the
whole lesson path is built and verified, then open one `main` pull request. The
owner's production acceptance happens after that pull request merges and the
macOS TestFlight build that carries the new card ships, in a chat bound to a
`READY` profile on the configured scope:

- **TG1:** ask for one lesson. The first `browser.act` card names the element
  and the page and offers **Allow for this task**; after one approval the
  lesson continues without further cards, and the banner counts actions and
  minutes.
- **TG2:** **Stop** on the banner ends the grant, and the next action shows a
  card.
- **TG3:** after allowing again, an action outside the lesson, such as a
  payment or settings control or leaving the scope, still shows a card that
  says why the permission does not cover it.
- **TG4:** the lesson's activity rows that the grant authorized show "Allowed
  by task permission" and what was clicked or typed.
