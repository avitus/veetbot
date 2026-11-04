# ADR-0294: An approved click can save a text file into the chat

- Status: Proposed. The owner asked for the capability on 2026-11-03; acceptance
  follows the production check in Validation.
- Date: 2026-11-03
- Amends: ADR-0058 (context options), ADR-0148's Limits (downloads)
- Related: ADR-0129, ADR-0130, ADR-0209, ADR-0253, ADR-0261; engineering plan Section 33
- Scope: one tool's output schema and version, one observation field and one
  service response limit; no gate, milestone, policy profile or compiled
  `policy_version` changes
- Detailed design: `docs/plan/browser-automation.md`

## Context

On 2026-11-03 the owner asked Chat to compile recurring charges from Chase.
Chat reached the account's activity page and, through approved clicks, asked
Chase for the transaction CSV. Chase reported the file ready, but the browser
context refuses and cancels every download, so nothing arrived. Chat said so,
and at the owner's direction read the activity page by page instead, one approved
click at a time. The owner then asked that Chat be able to download the file.

The plan's boundary is that downloads fail closed until a contract classifies
them, and every website-originated name and byte is `EXTERNAL_UNTRUSTED`. Three
further constraints shape the contract.

1. **The roster.** Browser tools are always defined and the definitions sit at
   their token cap (ADR-0253), so a new `browser.download` would move another
   tool out of the definitions. Chat also clicks a download control with
   `browser.act` already, and the failed run shows it will.
2. **Reading.** A file the model can reach must be read in bounded sections
   without a new approval, and must keep its untrusted label through the trust
   overlay. The capture reader of ADR-0261 already does exactly this.
3. **The projection.** A browser result over the inline budget reaches the model
   through a projection of its structured fields, so a note outside `structured`
   would never be seen.

## Decision

1. **A file needs an individually approved act.** While a `browser.act` click or
   key press runs, the runtime holds a one-file window. Only an act that carries
   no grant or research constraint may save. A file started outside that window,
   whether by a page on its own, a sign-in, a navigation or a read, and any file
   met by a grant-authorized or research-permitted act, is still cancelled. The
   file must also come from one of the login's origins, read from its URL, with a
   `blob:` URL read as the origin inside it. A file from anywhere else, such as an
   embedded third-party frame, is refused as `origin_not_allowed` and does not take
   the slot. A `data:` URL names no origin, so it is refused too. A grant-authorized act that met a file reports
   `needs_approval`. A control named with `download` or `export`, or a link with a
   `download` attribute, already reads as a file-transfer consequence
   (`browser_classification.py`), so it asks for approval.
2. **Only bounded text is admitted.** The file is at most 2 MiB, decodes as UTF-8
   or Windows-1252, holds no NUL byte, and has no PDF signature in its first
   kilobyte. It is normalized to UTF-8. Anything
   else, including PDF and spreadsheet files, is refused by name:
   `unsupported_type`, `too_large`, `timed_out` or `failed`. The size is checked
   while the file downloads, in a private directory under the session's temporary
   home, so a large file is cancelled rather than held. The file is removed from
   that directory once read. The website's suggested name is reduced to a bare
   name of letters, digits, `.`, `_` and `-`; the media type comes from a short
   list by suffix and is never taken from the website.
3. **The wait is bounded and starts only where it helps.** After the page settles,
   an act on a control whose accessible name contains `download`, `export` or
   `csv`, or a link with a `download` attribute, waits up to 10 seconds for the file
   to start; any started file then has up to 8 seconds to finish. Both fit
   `browser.act`'s 30 seconds. The name decides only whether to wait, never
   whether to admit. The window closes when the act returns, so a later file is
   cancelled.
4. **The bytes cross once, in the act's response.** `BrowserObservation` gains an
   optional `download`. The hosted service sends the file in base64 with the
   observation, and the worker's response limit grows by that envelope.
   The worker repeats the admission checks on what it parses, since the service
   handles hostile pages. Observation results list their fields explicitly, so
   the bytes never reach a tool result, an event or a log.
5. **The tool saves the file as a capture of the chat.** `browser.act@1.8.0`
   writes it through the run's artifact writer with origin `tool_output`, trust
   `EXTERNAL_UNTRUSTED` and the capture mark, so `artifact.read_capture` opens it
   under its existing session, expiry and scope rules, and it is deleted with the
   chat. The mark also says `literal`, so the reader takes the file as its own
   text and never as serialized tool output, which a JSON array would otherwise
   be mistaken for. The structured result gains `saved_file`, which the context projection
   always keeps. It carries the artifact ID, name, type, size, section count and
   the exact `tool.call` to read it, or `status: refused` with a reason. A chat
   whose plan cannot reach the reader gets `reader_unavailable` and nothing is
   stored.
6. **No new tool and no new definition cost.** The input schema and description
   are unchanged, so the definitions keep their tokens; only the output schema
   and version move. `browser.act@1.7.0` stays registered for chats pinned to it
   (`PinnedBrowserActTool`), and it reports no file, so a chat started before
   delivery behaves as it did: start a new one to use downloads.

## Validation

Real Chromium covers an attachment link, a script-built file that starts seconds
after the click, a grant-authorized click, a binary file, a file over the limit,
a file a page starts on its own, and an approved click with no file. Unit tests
cover encoding, naming and the refusals, the hosted response, the capture mark,
the writer and reader together, the projection, and the pinned spec.

The owner's production check: in a new chat bound to Chase, ask for recurring
charges, approve the Download clicks, and confirm Chat reads the CSV. Chase's page
has not been exercised, because nothing here signs in to the owner's account.
This ADR stays Proposed until that check passes.

## Limits

- The window is a span of time, so it cannot name a file's cause. A page that
  starts a file during an approved click takes the slot, and the file is saved as
  if the click had asked for it. Requiring cause would refuse the case this exists
  for: a banking page fetches the data and saves it later from a script, which
  carries no link to the click. The consequence is one bounded, untrusted text file
  from a site the owner connected and was acting on, which adds nothing the page
  could not already show the model as text. No page can trigger a save without an
  approved click in progress. This is a residual risk for the owner to accept with
  this ADR.
- Text files only. PDF statements and spreadsheets are refused and need their own
  contract. A QFX or OFX file is admitted as plain text.
- A file that starts more than 10 seconds after the click, or after the page has
  settled on a click whose control names no file, is cancelled and unreported.
- A file a popup or new window starts is not seen: popups stay closed.
- A site whose button saves through a `data:` URL, or serves the file from another
  of its own hosts, is refused until that host is added to the login.
- The capture reader's limits apply: 2 sections a step and 16 a run at 16 KiB, so
  a full read stops at about 256 KiB; `find` still searches the whole file.
- Chat reads the file itself. It has no sandbox import for computing over the rows,
  so a long file's totals rest on what the model reads.
- If the act's postcondition read fails, the act reports an uncertain outcome and
  the file is not kept, so it takes another approved click.
- The saved file does not attach to the reply as an owner download.
- The approval card names the click, not the file.
- No acceptance criterion or grant exclusion is weakened; this supplies the
  separate file-transfer contract Section 33 reserves, as ADR-0148 did for upload.
