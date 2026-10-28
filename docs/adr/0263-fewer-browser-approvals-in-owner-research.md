# ADR-0263: Fewer browser approvals in owner research

- Status: Proposed. The owner authorized implementation of decisions 1 to 4 on
  2026-10-25; acceptance follows the production check in Validation. A fifth
  change, letting clicks through after a memory, knowledge or mail read, is
  parked.
- Date: 2026-10-25
- Amends: ADR-0129, ADR-0209
- Scope: Existing browser authorization and isolated-runtime facts; no gate,
  milestone, policy profile or compiled `policy_version` changes
- Detailed design: `docs/plan/browser-automation.md`,
  `docs/plan/policy-and-approvals.md`
- Renumbered from ADR-0258 on 2026-10-27, when `main` took that number for
  deferred definitions and truncation recovery; earlier commits on `dev`
  call this decision ADR-0258

## Context

ADR-0209 lets ordinary interactions in an owner-selected READY profile run
without an approval card. Two owner-requested records lookups on a patient
portal still raised 72 `browser.act` approvals. The owner approved every one,
and none was for a consequential action: they were menus, navigation links,
tabs, a menu search, a filter check box and "view all" links. In the shorter
session the owner waited about 11 of the run's 17 minutes. The records show
four causes.

1. **The platform's own sign-in question disqualified the turn.** When a
   profile's session has expired, the runtime asks the owner to sign in with a
   `conversation.ask_user` call it adds itself, and resumes when the browser
   service verifies the sign-in. ADR-0129 G5 refuses research and task-grant
   authority to a turn that called any non-browser tool since the owner's
   message, so every later action in that turn asked. G5 exists because a turn
   that read mail, memory or search results could type them into a page. The
   sign-in question brings back only server-authored text, or the owner's own
   answer.
2. **Saving a file disqualified the turn the same way.** `artifact.export`
   returns only an artifact id, a size and an attachment flag, but it is not a
   browser tool, so evidence files saved during a review turned the rest of
   the review into per-click approvals.
3. **Icon controls had no readable target.** The portal draws its icons as
   `<use href="/…/library.svg#name">`, a same-origin external SVG sprite. Page
   script cannot read the copy such a reference renders, so ADR-0129 treats the
   element as reaching a target outside every origin. Every control carrying
   an icon (the menu button, menu links, tabs, the back link, filter buttons)
   was refused as `link_outside_prefix`, while plain text links passed. The
   sprite holds several thousand icon symbols and no link or embedded content.
4. **Two record descriptions read as actions.** The deny-biased consequence
   classifier read a clinical note link named "<note>, Updated <month> <day>,
   <year>, <clinician>" as a security change (`update`), and a result link
   named "Lab <test> REPORT <date> <clinician>" as publication (`report`).

Research refusals carried one undifferentiated code,
`browser.research.not_covered`, so production could not tell these apart.

## Decision

1. **An owner-answered question does not disqualify a turn.** A
   `conversation.ask_user` call counts toward G5's non-browser tools only
   until it has a result with `user` or `platform` trust: the owner's own
   answer, or the server-authored sign-in resumption. A pending question, or
   an answer with any other trust (a device-captured triage reply is
   `external_untrusted`), still counts. The rule applies wherever G5 applies:
   research authorization, task-grant authorization and task-grant offers.
2. **A file export does not disqualify a turn.** `artifact.export` is
   excluded from G5's non-browser tools, because its result carries only
   platform-known identifiers. A sandbox command that produced the exported
   file is a separate call and still counts.
3. **Research refusals name their reason, and the runtime inspects external
   icons.**
   - `BrowserResearchAuthorizer` refuses with
     `browser.research.<reason>` and the `browser_research` authorization
     kind. Reasons form a closed, content-free vocabulary: the run, turn,
     provenance, profile, observation and policy checks, plus every
     task-grant coverage reason. When no other authority applies, the
     composite authorizer returns a task grant's specific denial first, then
     research's, then the first denial. The approval record keeps the reason
     as `research_not_covered`. Approval responses do not carry it, as they do
     not carry the rule that fired.
   - When an element read meets an external `<use>` reference, the isolated
     runtime asks Chromium for the page's DOM through the DevTools protocol,
     once per observation or live check, and reads the element again. That
     read happens outside the page's script realm and includes each `<use>`
     element's rendered copy. One reference names one
     copy within a document, so each `<use>` naming a single reference
     witnesses that copy. A resolved external reference counts as inspected
     only when it is on the page's origin and every witness's copy is loaded
     and, through nested copies, holds no link, frame, object, embed, foreign
     object, image map or animation element, within the existing 2,048-node
     bound. A `<use>` naming two references (differing `href` and
     `xlink:href`) witnesses neither, and a page with any animation element
     inspects nothing. The page-side read then treats an inspected reference
     as it treats a fragment reference whose target it can see, and requires
     every reference an element names to qualify. Any other external
     reference stays opaque: one that is cross-origin, not yet loaded,
     unsafe, or changed between the inspection and the read. A live check
     inspects the live page and re-arms its guarded read after the
     inspection. A DevTools failure leaves every external reference opaque.
4. **Two descriptive phrases stop matching.** In each reading of a label,
   `updated` immediately followed by a month name or abbreviation dates a
   record, and `report` or `reports` immediately after `pathology`, `lab`,
   `laboratory`, `radiology` or `imaging` names a document. Neither token
   then yields a consequence. Every other form and context keeps its
   category: "Update email", "Report spam", a `report` path segment, and
   "Request Refills" (payment) still ask. No other vocabulary changes.

Unchanged: G5 for mail, memory, knowledge, search, sandbox and every other
tool, the parked fifth change, named-consequence approval, sensitive paths,
typed-text rules, the live recheck, `BROWSER_RESEARCH_ENABLED=0`, standing-grant
ceilings, task-grant scopes and limits, and the compiled policy. No
`policy/default.yaml` edit is made, so `policy_version` and the formation
release evidence bound to it are unchanged.

## Consequences

- A research task interrupted by an expired sign-in continues without
  per-click cards after the owner signs in.
- Icon controls on sites that draw icons from a same-origin sprite are
  covered once Chromium shows their copies are inert. An observation or live
  check that meets an external `<use>` spends one DevTools document read:
  about 22 ms, measured locally on the portal's public sign-in markup with
  120 added sprite icons and its 1.25 MB sprite. Other pages spend nothing.
  The read grows with the page, about 0.05 ms and 400 bytes per element
  (20,000 elements in 0.4 s as 8 MB; 200,000 in 11.6 s as 79 MB), so a page
  past 20,000 elements is not read and its external references stay opaque.
  The click guard cannot see inside a user-agent shadow root, and a page can
  point a `<use>` elsewhere and back, or its origin can serve the same URL
  with new content, after the live read. So a grant-constrained click whose
  live read relied on inspected copies reads them again immediately before
  the click and is refused, asking instead, when any is no longer inert. That
  second read is not atomic with the click; the window it leaves is the
  runtime's own dispatch, with no page turn in between. An element read lists
  at most 64 external references; a read that met more, or a live read with
  no overflow flag, is refused before options are read, since the complete
  reference list was not inspected and rechecked, and such an element reads
  as opaque in observation facts, so a task grant refuses it before spending
  a use. A standing grant's ceiling refuses a click or key whose link target
  the runtime read leaves the origin, or whose element it could not bound.
- A page with any SMIL animation element inspects no sprite, so its icon
  controls still ask. The portal's sign-in page and sprite have none.
- Approval records say why research mode refused, so a remaining prompt can
  be traced without page content.
- Two narrow exceptions enter a deny-biased vocabulary. A hostile label could
  phrase a security change as "Updated <month>" or a publication as
  "Lab report"; the same-origin, sensitive-path and live-page checks still
  bound it, and each label source is still classified separately.

## Validation

Red tests precede each change: active-turn facts for an answered, pending,
untrusted and platform-resumed question and for a file export; a pipeline run
in which the owner answers a question and the next ordinary act needs no card;
the research refusal reason on the approval record; real-Chromium pages with a
same-origin sprite icon in a button and in a link (covered), a sprite symbol
holding a link, a cross-origin sprite, a missing symbol, an element naming two
references, an animated page, and a reference changed after observation (each
refused); synthetic DevTools trees for what Chromium leaves out of a copy; and
classifier cases for both phrases beside the controls that must still ask.
`make check` on the sidecar gates delivery.

Production acceptance: after delivery, rerun the owner's records lookup,
including one with an expired sign-in. Accept when it finishes with no card for
ordinary navigation, and a refill request or message control still asks.
Approval records from the run show any remaining `research_not_covered`
reason.
