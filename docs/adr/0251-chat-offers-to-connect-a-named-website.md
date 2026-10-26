# ADR-0251: An unconnected chat offers to connect a website the owner names

- Status: Accepted by the owner on 2026-10-25 with both recommended choices (a
  card, never automatic connection; an offer expires at the next owner message
  or 24 hours after its source message); implemented 2026-10-25, delivery pending
- Date: 2026-10-25
- Amended: 2026-10-25, with the implementation. The source run's row states
  facts only, matching the existing "data only" runtime row: the login is
  saved, the chat is not connected to it, and a Connect or Switch button
  connects it and continues the request. The continuation's seed is an owner
  message the app writes on the tap: `user.message.created` with the
  principal actor, so existing owner-authority checks hold, and an `origin`
  of kind `browser_offer`. Memory formation reads it like any owner message;
  its fixed text names only the website's host. "Not now" hides an offer on
  that device; the server offer still closes by the rules in decision 4. The
  public suffix list stays inside the isolated browser service (ADR-0128), so
  the registrable domain is approximated: the last two labels, or three
  under a two-letter country code whose second level is `ac`, `co`, `com`,
  `edu`, `gov`, `net` or `org`
- Amends: ADR-0130, ADR-0172
- Related: ADR-0094, ADR-0128, ADR-0129, ADR-0141, ADR-0164
- Scope: The existing browser capability (the Milestone 10 browser tranche).
  No milestone or gate moves, and no policy profile changes.
- Detailed design: `docs/plan/browser-automation.md` and
  `docs/plan/http-api-and-streaming.md`

## Context

Hosted composition resolves the browser profile per chat. A chat advertises
browser tools only when its trusted session metadata holds a selected profile;
otherwise the frozen plan leaves them out (ADR-0130). Only the owner selects a
profile, from Website Access in that chat or with a message, and a
model-authored message can never select one (ADR-0172). The application
resolves a profile for the owner in one case only: an exact X follow command
connects the single owned X profile, or holds the run on a platform question
until the owner connects one (ADR-0172).

On 2026-10-25 the owner opened a new chat on the Mac and asked the agent to
sign in to a patient portal. The owner had saved that portal in Website Access
and signed in to it on 2026-10-21, and its profile was still Ready. The chat had
no website, because the device had no default for new chats. Without browser
tools, the model first said the website tool was unavailable and asked for
uploaded documents. When the owner replied that the agent had access, it told
them to "reconnect or reselect" the site in Website Access. The app loaded the
website list during the chat, but no connection followed. In Website Access,
the portal's only action reads "Use for new chats". That action does connect
the open chat, but the label does not say so. The same thing happened on
2026-10-20: twice the owner said they had already signed in, and the site was
connected to the chat more than an hour after the first request.

There are two gaps:

1. When the owner names a saved site, nothing tells them that this chat is not
   connected to it, and nothing offers the one action that fixes it.
2. In an unconnected chat the model does not know the remedy, so it improvises,
   for example by asking for uploads.

A device default does not close either gap. A chat holds one profile, so a chat
that inherited the default X or shopping profile still lacks the portal.

## Decision

1. **Matching owner text to an owned website.** The matcher runs when the
   service admits an owner-authored message from an authenticated native
   client (the principal actor, ADR-0172) in a chat whose composition resolves
   profiles per session. It compares the message text with the owner's own
   website profiles whose status is Ready, Authentication required or Needs
   user. It reads only the text the owner typed. It never reads attachments,
   earlier messages, memory, tool results, page text, scheduled instructions,
   delegated input, or messages from inbound surfaces (Telegram, WhatsApp, SMS).
   The matcher is deterministic and runs before the model:
   - Each allowed origin yields its host, its registrable domain, the
     leading label of that domain with hyphens
     removed (`examplehealth` for `example-health.org`), and the host's first
     label when it is not `www`.
   - The message matches a profile when it contains the host or the
     registrable domain. It also matches when one to three consecutive words,
     reduced to letters and digits, join to the domain label ("Example Health"
     gives `examplehealth`) or to a first host label of at least seven letters
     ("My Chart" gives `mychart`), or when a single word of at least five
     letters begins a domain label of at least five letters ("Example").
   - A domain label shorter than four characters matches only by host or
     registrable domain, so the letter x never matches `x.com`.
   - The matcher offers at most three profiles, longest matched text first. It
     offers nothing for the profile this chat already uses.

2. **A match records an offer and never connects.** A match appends a
   `session.browser.offered` event. The event carries an offer ID, the
   sequence of the source message, and the IDs and origins of the matched
   profiles. The message is admitted, and its run proceeds as usual. The chat's
   tool surface and run limits do not change, and no run waits on the offer. A
   site's name is not a request to use the site. So unlike the X follow
   command, a match neither connects a unique profile nor holds the run.
   ADR-0172 keeps both of those behaviors for exact follow commands only.

3. **The model learns the remedy, not the profile.** For the source run, the
   context carries a platform-trust row. Like ADR-0130's origin row, it sits
   outside the frozen prefix. The row says that the owner has a saved login for
   the named origin, that the origin is not connected to this chat, and that
   the app shows the owner a Connect button. It tells the model: if the request
   needs that site, say in one sentence that the work continues once the owner
   connects it, and never ask for credentials or uploads. When the chat uses
   another profile, the row names the website the chat is connected to. The row
   never contains a profile ID. In an unconnected chat with no offer, a shorter
   row says that the owner connects a website from Website Access in this chat.
   Neither row is a tool, and neither grants anything.

4. **The native card.** On iPhone, iPad and Mac, the app shows the offer under
   the reply once the source run has ended. The card reads "Connect <host> to
   this chat", or "Switch this chat to <host>" when the chat uses another
   profile. Its buttons are "Connect and continue" and "Not now". For a profile
   that needs sign-in, the first button reads "Sign in and continue" and starts
   the existing sign-in flow first (ADR-0128). The card goes away when the owner
   uses or dismisses it, sends a new message, or connects the chat some other
   way. It also goes away when the profile is removed, or 24 hours after the
   source message.

5. **Accepting the card continues the request.** "Connect and continue" sends
   `PUT /v1/sessions/{session_id}/website` with the profile ID and an optional
   `offer_id`. The service takes the session admission lock and checks for an
   open offer that names this profile, with no active run in the chat. It then
   connects the chat under ADR-0172's selection rules, and admits one
   continuation run. The run's seed is a `user.message.created` event that
   the app writes on the tap, with the fixed text "I connected <host> to this
   chat. Please continue my earlier request." and an `origin` of kind
   `browser_offer` that names the offer and its source message.
   - The owner's authenticated tap admits the run. It therefore records the
     principal actor, has interactive authority (ADR-0172), and carries the
     tap's scopes, like any owner message.
   - The first acceptance consumes the offer, and a replay returns the same
     run. An expired, superseded or foreign offer returns
     `409 browser_offer_not_open` and connects nothing.
   - Without an `offer_id`, the endpoint behaves exactly as it does today.

6. **Website Access says what its button does.** When Website Access is opened
   from a chat, a profile row's action reads "Use in this chat", and the
   connected profile shows "Connected to this chat". The device default for new
   chats becomes a separate, labeled control.

7. **What does not change.**
   - Browser tools still appear only in a connected chat (ADR-0130), and only
     the owner connects one.
   - Policy, approvals, task grants (ADR-0129) and task approval websites
     (ADR-0141) are unchanged. A click on the portal still needs approval unless
     the owner adds the site to the task approval websites.
   - No policy profile changes, so formation evidence bound to the profile hash
     still holds.
   - The X follow flow keeps its own rules.

## Consequences

- The owner connects the site with one tap instead of explaining the problem
  and then going to settings, and the model's first reply names that action.
- When the owner mentions a saved site in passing, a card appears that they can
  ignore. A run never waits on an offer.
- Indirect references, such as "my doctor's portal", match nothing. The
  unconnected-chat row in decision 3 makes the model point to Website Access
  instead. Owner-chosen names for a login would close this gap; they are
  deferred.
- The model learns only the origin the owner named, and only in that run. It
  never sees the list of the owner's other saved sites.
- The event log gains one event type, which the conversation projection skips.
  An unconnected chat's context gains one short row outside the prefix, so the
  cache does not rotate.
- A continuation run uses the browser-task limits (ADR-0130) because the chat is
  now connected.

## Alternatives considered

- **Connect a unique exact match automatically, as X follow does.** Rejected:
  naming a site in prose does not consent to browsing it, and connecting changes
  the chat's tool surface and run limits. A wrong connection made from a text
  heuristic costs more than a card the owner ignores. The owner chose the card
  (owner decision 1).
- **Hold the run on a platform question before the model runs, as the X follow
  wait does.** Rejected: every passing mention would block a run.
- **A model-callable tool that requests a connection card.** A card grants
  nothing until the owner taps it, so the tool would be safe. But the model
  would have to see the owner's saved sites or guess origins. Deferred; revisit
  if indirect references turn out to be common.
- **Put the owner's saved sites in every chat's prompt.** Rejected: it adds the
  list of the owner's logins to every prompt for a rare need.
- **Only relabel Website Access.** Necessary (decision 6), but on its own it
  leaves the model's reply and the extra trip through settings.

## Owner decisions

The owner accepted both recommendations on 2026-10-25:

1. Even a unique match on the host or registrable domain produces a card and
   never connects the chat automatically.
2. An offer expires at the next owner message in the chat, or 24 hours after
   its source message, whichever comes first.

## Verification

Test-first regressions cover:

- **Matcher:** matches on the host, the registrable domain, joined words, the
  first host label, and the five-letter prefix. Short labels match only by host.
  At most three profiles are offered. Attachments, earlier messages, inbound
  surfaces, scheduled and delegated runs, tool results and memory are ignored.
- **Admission:** a match records an offer. The run, the tool surface and the
  run limits are unchanged. Nothing is offered for the profile the chat already
  uses. A chat that uses another profile gets a switch offer.
- **Context:** the row's content is checked. It contains no profile ID, and it
  sits outside the prefix.
- **Acceptance:** the chat is connected and exactly one continuation run is
  admitted, with the principal actor. A replay returns the same run. Consumed,
  expired, superseded and foreign offers, an active run, and the sign-in path
  each return the documented result.
- **Native:** the card's states and the relabeled Website Access row on iPhone,
  iPad and Mac.

Production acceptance: in a new, unconnected chat, naming the saved portal shows
the card, and one tap continues the original request.
