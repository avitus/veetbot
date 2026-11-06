# ADR-0302: One approval for a request on a signed-in website

- Status: Proposed. The owner set the principle and its four choices on
  2026-11-05; acceptance follows the production check in Validation.
- Date: 2026-11-05
- Amends: ADR-0129 (who may be offered a grant, and its lifetime and
  generation pin), ADR-0164 decision 3 (a grant across this run's own verified
  sign-in), ADR-0263 G5 bookkeeping, ADR-0294 (the per-file card)
- Supersedes: ADR-0300 decision 1 (the closed list of Chase activity controls)
- Related: ADR-0058, ADR-0127, ADR-0141, ADR-0209
- Scope: browser authorization, the `browser.act` approval offer, one approval
  resolution and offer field in the API, one dispatch-constraint kind, and the
  Apple approval card. No table, migration, milestone, gate, compiled policy or
  `policy_version` change.
- Detailed design: `docs/plan/browser-automation.md`,
  `docs/plan/policy-and-approvals.md`, `docs/plan/http-api-and-streaming.md`

## Context

On 2026-11-05 at 7:27 PM Pacific the owner asked a chat bound to their Chase
login to review two years of card transactions for recurring expenses. The
run finished at 8:04 PM with 94 model calls and USD 12.77 spent. It raised 21
approval cards, and the owner spent about 22 of the 37 minutes deciding them.
Only two cards were for the clicks that actually saved a file. The last card
waited 7 min 47 s, after which the browser session was gone, so November and
December 2024 were never read.

Each card had a cause that an earlier decision was meant to remove:

- A clock read at step 1 made the turn "bookkeeping". Research authority
  (ADR-0209) then applied only on the URLs ADR-0300 lists, and the account
  overview page is not among them. The clock read also disqualified every
  task-grant offer, so none of the 21 cards offered one.
- Reading the first downloaded CSV with `artifact.read_capture` made every
  later act ask, because capture reads are outside the bookkeeping set.
- ADR-0300 lists Chase controls by exact label. After a download Chase
  relabels the dropdown "Activity, Since last statement", and the list holds
  neither that nor "Last year", "Year to date" or the statement choices.
- The word "download" makes "Download other activity", a form reset, a file
  transfer. "Download account activity" read as a security change.

Every one of these is a rule that is correct by itself. Together they ask
dozens of times during a read-only task on a site the owner chose to sign in
to. Adding more exceptions, one site or label at a time, does not converge.

The owner's decision, 2026-11-05: "This is a read-only operation on a trusted
website. It should be one approval all the way through." The owner then chose:

1. Any website with a Website Access login qualifies. Signing in there is what
   makes a site trusted; there is no separate list.
2. One approval lasts until the request is done, including pauses to sign in
   again, with a 2-hour cap. A follow-up message asks once more.
3. Downloads are included.
4. After the request has read mail, memory, knowledge or the web, Veetbot may
   still type dates, numbers and short searches without asking. Free text then
   asks. The clock, the run's notes and files it downloaded no longer count as
   outside reads.

This loosens security rules in `browser-automation.md` (sensitive page paths,
per-file download approval, the generation pin, G5). The owner approved that in
the conversation that produced this record.

## Decision

1. **A request approval.** Every `browser.act` card in an eligible run offers
   "Allow for this request" beside Allow once and Deny. Accepting it approves the
   pending action and, in the same transaction, creates a request grant.
   - An eligible run is an interactive top-level run in a session bound to a
     READY Website Access profile with no schedule binding, seeded by the
     owner's own message. Scheduled runs, inbound and device-ingested sessions
     and child runs never receive an offer or use a grant.
   - The offer is made only when the pending action would itself be covered
     (decision 2), so a card for a payment never offers it.
   - No scope list applies. The grant's origins are the profile's allowed
     origins at creation, taken from the profile, never from the page.
   - The approval itself is the grant: the new resolution `approve_for_request`
     approves the pending action and records `browser.request_grant.created`
     with the approval id. The offer is stored on the approval as
     `request_grant_offer`, beside the ADR-0129 `task_grant_offer`, so a client
     that predates it shows Allow once and Deny and nothing else changes for
     it. The resolution requires `approval.resolve` and `browser.grant.write`,
     and repeats the offered profile id. Nothing else creates a request grant,
     and no table or route is added.
   - Research authority (ADR-0209, ADR-0263) still covers what it covers with no
     card. The request grant is for everything research leaves to a card.

2. **What it covers.** Click, select, check, press, scroll and type (decision
   3) on any page of the grant's origins, and a click that starts a download.
   - A page path never refuses an action by itself. Authenticated sites keep
     every view under one path such as `/web/auth/dashboard`, so a sensitive
     page path says nothing about the control. A link or form target must stay
     within the grant's origins; leaving them asks.
   - The consequence ceiling is `unknown` plus inbound `file_transfer`. A saved
     file follows ADR-0294 and ADR-0300 decision 2 unchanged: 2 MiB of text into
     this chat, the same capture rules and reader. Only the per-file card goes.
   - Still asking every time: payment, purchase, subscription and money
     movement; messages, posts and publication; account, settings, security and
     authentication changes, including sign-out; permissions; legal acceptance;
     deletion; uploads and file inputs; credential, one-time-code, payment and
     identity fields; a new window or a target on another origin.
   - Every other ADR-0129 check stays: the worker decides on facts from the
     observation that named the element, the isolated runtime repeats the
     classification and coverage on the live page and can only refuse, the click
     and focus guards, document-request refusal outside the origins while the
     act runs, revision fences and non-replay of uncertain actions.

3. **Typing, and which reads count.** G5 (ADR-0129, ADR-0263) now governs only
   free text.
   - These never count as outside reads: `system.current_time`,
     `context.update_working_state`, `math.calculate`, `artifact.export`, an
     answered `conversation.ask_user` (ADR-0263), and `artifact.read_capture`.
     A capture is saved into this chat by an earlier approved action, whose own
     tool already counted in its turn; reading it again adds no more than the
     conversation history the model already holds.
   - Always covered: a valid calendar date in a common written form, an amount
     up to 9,999,999.99 (optionally with a currency sign), and a search of at
     most 40 characters of letters, digits, spaces and `.,'-&` with at least one
     letter and at most four digits in all. None of these can hold an address,
     a link, an account or card number, or a credential.
   - Other text of at most 256 characters is covered only while the request has
     made no outside read: mail, memory, knowledge, people, web search or page
     extraction, MCP tools, or anything not listed above.
   - The ADR-0129 typed-text rules otherwise stay, including the credential-shape
     check and the refusal of credential fields.

4. **Lifetime.** The grant belongs to the run that answers the owner's message.
   - It ends when that run ends, after 2 hours, when the owner stops the
     request, or when the profile is revoked, deleted, no longer READY or no
     longer allows a grant origin. A follow-up message starts a new run and
     asks again.
   - The run's own budgets (steps, cost, deadline) bound how many actions it
     takes; the grant adds no counter of its own.
   - It survives this run's own sign-in pause. A changed profile generation is
     accepted only when the profile's newest sign-in started after the approval
     and is the one a `browser.authentication.resumed` event of this run names
     (ADR-0164 decision 3). Any later sign-in, including one started from
     Settings, ends it. A sign-in from Settings followed by this run's own
     sign-in keeps it: both are the owner signing in to the same login.
   - A run has at most one live request grant; it never transfers to another
     run, session or profile.

5. **One descriptive phrase stops matching.** As in ADR-0263 decision 4:
   `account` or `accounts` immediately followed by `activity`, `statement`,
   `statements`, `summary`, `details` or `history` names a record, so
   "Download account activity" is a file transfer, not a security change.
   Every other form keeps its category ("Manage account" still asks). The
   phrase applies wherever the classifier runs, as ADR-0263's did.

6. **The card says what the owner allows.** The offer text is server-authored:
   "Allow for this request: open pages, choose menus and filters, type dates and
   searches, and save downloaded files into this chat on <origins>. Veetbot still
   asks before payments, transfers, messages, account or security changes,
   deletions, uploads and passwords. It recognises those by the website's
   labels." Once allowed, the card reads "Allowed for this request". Stopping
   the request in the chat ends the grant with the run. Creation is audited by
   `approval.resolved` and `browser.request_grant.created`; each use by
   `tool.call.authorized` with `authorization_kind: "request_grant"` and the
   approval id.

7. **ADR-0300's Chase control list becomes redundant.** Decision 2 covers those
   controls on every site without naming any. The list stays until the
   production check below passes on Chase, and is then removed in its own
   change. ADR-0300 decisions 2 and 3 (the CSV observer and the timeout report)
   stay.

## Consequences

- The Chase review asks at most once after any sign-in. The 7-minute stall that
  lost two months of data cannot recur from a per-click card.
- Inside a signed-in site the boundary is now the consequence classifier, not
  the page path. A control that performs a write but carries no write word in
  any label source (for example a bare "Confirm" after the agent filled a
  transfer form) is covered. The form fields that lead there (amount, payee,
  account number) are payment or identity fields, which still ask, and typing
  long free text after outside reads asks.
- A hostile page on a trusted origin can still choose what an `unknown` click
  does, as with task grants. Origins, the live recheck and the non-transfer of
  grants bound it.
- Clients older than the update show Allow once and Deny only. The request grant
  needs the new Apple build.
- Labels that carry payment words in a reading context, such as "Card details"
  or "Credit cards" on a card issuer's site, still ask; the deny-biased
  vocabulary is unchanged apart from decision 5.
- If the Apple app is updated but `BROWSER_RESEARCH_ENABLED=0`, no offer is
  made and every card is the ordinary one.
- Agent guidance (going straight to presets instead of typed date ranges) is a
  separate change; it cuts steps, not cards.

## Validation

Red tests first: the offer on a card in an eligible run and its absence for a
payment control, a scheduled run, a child run and a profile that is not READY;
coverage of a sensitive-path page, an "Activity, Since last statement" dropdown,
"Last year", "Download other activity" and "Download account activity"; payment,
transfer, message, settings, delete, upload and credential controls still asking;
date, number, search and free-text typing before and after a mail read and after
clock, notes and capture reads; the generation move on this run's verified
resumption and the end on any other sign-in; the 2-hour window and the run
binding; refusal by the isolated runtime when the live page differs. Real-Chromium
cases run through `make test-browser-cases`.

Production acceptance: rerun "Review my Chase credit card transactions over the
past two years and identify any recurring expenses". Accept when the run raises
at most one card besides sign-in, saves every year it needs, and a payment
control on the same site still asks.
